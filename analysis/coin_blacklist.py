#!/usr/bin/env python3
"""Can persistently losing coins be weeded out? Tested causally, not in-sample.

This is the most overfit-prone filter in the whole toolbox. 177 coins over ~2,300 events is
~13 trades each; against ~250bps of per-trade noise, "persistent loser" is statistically
indistinguishable from "unlucky". Ranking coins on the full sample and excluding the bottom
will ALWAYS look good on that same sample and means nothing.

So two gates, in order:

1. PERSISTENCE. Split the history in half by time and correlate each coin's mean return in
   the first half against its mean in the second. If that correlation is ~0 then coin-level
   performance does not carry over and no blacklist of any construction can work. This is
   the same logic that settled the ats question -- test whether the signal has information
   before testing whether a rule built on it makes money.

2. CAUSAL RULE. Walk forward: at each event, use only that coin's PRIOR trades to decide
   whether to take it. Skip if the coin's running mean is below a threshold after at least
   MIN_N observations. Nothing from the future is consulted, so the resulting P&L is
   achievable rather than fitted.

A random-shuffle control is included, because a causal rule can still produce an apparent
edge from noise -- the blacklist correlates with recent drawdown, and drawdown mean-reverts.

  python3 analysis/coin_blacklist.py [15m|1h]
"""
import math, random, sys
import numpy as np
import pandas as pd

IV = sys.argv[1] if len(sys.argv) > 1 else "1h"
CFG = {"15m": ("hyperliquid_15m_allperps.csv", 96, 32, 1500),
       "1h":  ("hyperliquid_1h_history.csv", 24, 8, 400)}
CANDLES, WIN, BACKSTOP, MINBARS = CFG[IV]
VOL_MULT, RV_PCTILE, COST_BPS = 5.0, 0.60, 3.0
random.seed(11)

print(f"building events ({IV}) ...")
df = pd.read_csv(CANDLES).sort_values(["symbol", "open_time_ms"]).reset_index(drop=True)
uni = pd.read_csv("perp_universe.csv").set_index("name")["day_notional_vol"]
qs = uni.quantile([1/3, 2/3]).values
fund = pd.read_csv("hyperliquid_funding.csv").sort_values(["symbol", "time_ms"])
fmap = {s: (g["time_ms"].values, g["funding_rate"].values)
        for s, g in fund.groupby("symbol", sort=False)}

rows = []
for sym, g in df.groupby("symbol", sort=False):
    if len(g) < MINBARS or uni.get(sym, 0) < qs[0]:
        continue
    g = g.reset_index(drop=True)
    cl = g["close"].values.astype(float); hi = g["high"].values.astype(float)
    lo = g["low"].values.astype(float);  vo = g["volume"].values.astype(float)
    tm = g["open_time_ms"].values
    med = pd.Series(vo).shift(1).rolling(WIN).median().values
    ph = pd.Series(hi).shift(1).rolling(WIN).max().values
    pl = pd.Series(lo).shift(1).rolling(WIN).min().values
    lr = np.full(len(cl), np.nan); lr[1:] = np.log(cl[1:]/cl[:-1])
    rv = pd.Series(lr).rolling(WIN).std().values
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = vo/med
    brk = np.where(cl > ph, 1, np.where(cl < pl, -1, 0))
    ft, fr = fmap.get(sym, (None, None))
    if ft is None:
        continue
    for i in np.nonzero((vr >= VOL_MULT) & (brk != 0) & ~np.isnan(rv))[0]:
        if i + BACKSTOP >= len(cl):
            continue
        j = np.searchsorted(ft, tm[i], side="right") - 1
        if j < 0 or (1 if fr[j] > 0 else (-1 if fr[j] < 0 else 0)) != brk[i]:
            continue
        d = -int(brk[i]); entry = cl[i]; ret = None
        for k in range(1, BACKSTOP+1):
            c = cl[i+k]
            if (d < 0 and c < ph[i]) or (d > 0 and c > pl[i]):
                ret = d*(c-entry)/entry; break
        if ret is None:
            ret = d*(cl[i+BACKSTOP]-entry)/entry
        rows.append(dict(t=tm[i], sym=sym, rv=rv[i], y=ret*1e4 - COST_BPS))
ev = pd.DataFrame(rows).sort_values("t").reset_index(drop=True)
ev = ev[ev["rv"] >= ev["rv"].quantile(RV_PCTILE)].reset_index(drop=True)
n = len(ev)
print(f"events: {n:,}  coins: {ev.sym.nunique()}  "
      f"median trades/coin: {ev.groupby('sym').size().median():.0f}")
print(f"baseline: {ev.y.mean():+.1f} bps/trade, total {ev.y.sum():+,.0f}\n")


def st(v):
    k = len(v)
    if k < 2: return (float("nan"), float("nan"), k)
    m = v.mean(); sd = v.std()
    return (m, m/(sd/math.sqrt(k)) if sd > 0 else float("nan"), k)


# ---------- gate 1: does coin performance persist? ----------
print("=== 1. PERSISTENCE: does a coin's first-half mean predict its second-half mean? ===")
cut = ev["t"].quantile(0.5)
h1, h2 = ev[ev.t <= cut], ev[ev.t > cut]
for MIN_N in (3, 5, 8):
    a = h1.groupby("sym").y.agg(["mean", "size"])
    b = h2.groupby("sym").y.agg(["mean", "size"])
    j = a.join(b, lsuffix="_1", rsuffix="_2", how="inner")
    j = j[(j["size_1"] >= MIN_N) & (j["size_2"] >= MIN_N)]
    if len(j) < 10:
        print(f"  min {MIN_N} trades/half: only {len(j)} coins qualify"); continue
    r = np.corrcoef(j["mean_1"], j["mean_2"])[0, 1]
    tr = r*math.sqrt((len(j)-2)/max(1e-12, 1-r*r))
    # what a bottom-half exclusion chosen on h1 would have done in h2
    bad = set(j[j["mean_1"] < 0].index)
    keep = h2[~h2.sym.isin(bad)]
    print(f"  min {MIN_N}/half: {len(j):>3} coins   corr(h1, h2) = {r:+.3f}  t={tr:+.2f}   "
          f"| excluding h1 losers -> h2 {keep.y.mean():+.1f} bps "
          f"(vs {h2.y.mean():+.1f}), {len(keep)}/{len(h2)} trades")
print("  A correlation near zero means coin identity carries no information across time,")
print("  and no blacklist can work regardless of how it is built.\n")

# ---------- gate 2: causal walk-forward blacklist ----------
print("=== 2. CAUSAL RULE: skip a coin whose OWN prior trades average below a threshold ===")
print(f"  {'min_n':>6} {'thresh':>8} {'kept':>7} {'skipped':>8} {'bps/trade':>10} "
      f"{'t':>6} {'total':>10} {'vs base':>9}")
base_tot = ev.y.sum()
syms = ev.sym.values; ys = ev.y.values
results = {}
for MIN_N in (3, 5, 10):
    for THR in (0.0, -20.0, -50.0):
        run_sum = {}; run_cnt = {}
        keep_mask = np.ones(n, dtype=bool)
        for i in range(n):
            s = syms[i]
            c = run_cnt.get(s, 0)
            if c >= MIN_N and (run_sum[s]/c) < THR:
                keep_mask[i] = False
            run_sum[s] = run_sum.get(s, 0.0) + ys[i]
            run_cnt[s] = c + 1
        kept = ev[keep_mask]
        m, t, k = st(kept.y)
        results[(MIN_N, THR)] = kept.y.sum()
        print(f"  {MIN_N:>6} {THR:>+8.0f} {k:>7} {n-k:>8} {m:>+10.1f} {t:>+6.1f} "
              f"{kept.y.sum():>+10,.0f} {kept.y.sum()-base_tot:>+9,.0f}")

# ---------- shuffle control ----------
print(f"\n=== 3. SHUFFLE CONTROL: same causal rule, coin labels randomised (20 runs) ===")
print("  a blacklist keyed to a coin's own drawdown can profit from mean reversion alone,")
print("  so the real rule must beat this")
MIN_N, THR = 5, 0.0
outs = []
rng = np.random.default_rng(11)
for _ in range(20):
    sh = rng.permutation(syms)
    run_sum = {}; run_cnt = {}
    km = np.ones(n, dtype=bool)
    for i in range(n):
        s = sh[i]; c = run_cnt.get(s, 0)
        if c >= MIN_N and (run_sum[s]/c) < THR:
            km[i] = False
        run_sum[s] = run_sum.get(s, 0.0) + ys[i]
        run_cnt[s] = c + 1
    outs.append(ev[km].y.sum())
outs = np.array(outs)
real = results[(MIN_N, THR)]
pct = (outs < real).mean()*100
print(f"  shuffled totals: p05 {np.percentile(outs,5):+,.0f}  median "
      f"{np.percentile(outs,50):+,.0f}  p95 {np.percentile(outs,95):+,.0f}")
print(f"  real rule (min_n={MIN_N}, thresh={THR:+.0f}): {real:+,.0f}  -> {pct:.0f}th percentile")
print(f"  baseline (no filter): {base_tot:+,.0f}")
print()
print("Verdict rule: the causal filter must beat BOTH the no-filter baseline and the 95th")
print("percentile of the shuffle. Anything less is drawdown mean-reversion or noise.")
