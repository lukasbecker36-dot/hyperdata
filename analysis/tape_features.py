#!/usr/bin/env python3
"""Attach tape-derived flow features to every event, then test them at full sample size.

vpin_edge.py tested three features on 273 filled trades and could resolve nothing under
~124bps. This runs the same question over 4,833 events -- every 5x spike + breakout in
the tape window, whether or not the bot took it -- which brings the resolvable effect
down to ~29bps.

Features, all computed strictly from prints BEFORE the decision moment (the signal bar's
close, which is when the bot acts), so none of them can see their own outcome:

  vpin30/60    |buy-sell| / (buy+sell) over the trailing 30/60 min. Replicates
               live_bot_ats.toxicity exactly, including that the window ends at bar close
               and therefore contains the spike bar itself.
  adverse_ofi  the signed version, multiplied by breakout direction: positive means flow
               is running INTO the fade.
  ats_tape     mean print notional in the signal bar, against its own trailing 24h
               median. This is ats_ratio measured PROPERLY: the live version divides
               candle volume by candle trade-count, which is a proxy for the same thing.
               Since ats sizing is now live off a tail effect, whether the proxy or the
               real distribution sorts better is worth money.
  size_conc    n * SUM(x^2)/SUM(x)^2 over the signal bar, where x is print notional. 1.0
               means every print was the same size; higher means a few prints dominated.
               Mean size cannot distinguish "ten $1k prints" from "one $10k print and
               nine $100 prints"; this can.
  big_share    share of signal-bar notional in prints over $10k.
  prints_ratio signal-bar print COUNT against its trailing median -- the "many small
               trades" leg of the cascade story, independent of size.

Same pass criteria as vpin_edge.py: monotone, both halves same sign, unconcentrated, not
redundant, and |t| >= 3 given the number of looks.

  python3 analysis/tape_features.py events.csv buckets.csv.gz
"""
import math, sys
import numpy as np
import pandas as pd

EV = sys.argv[1] if len(sys.argv) > 1 else "tape_events.csv"
BK = sys.argv[2] if len(sys.argv) > 2 else "tape_buckets.csv.gz"
BAR_MS, BUCKET_MS, WIN = 900000, 300000, 96

ev = pd.read_csv(EV)
bk = pd.read_csv(BK)
print(f"{len(ev):,} events, {len(bk):,} buckets, {bk.coin.nunique()} coins")

bk["ntl"] = bk.buy_ntl + bk.sell_ntl
bk["bar"] = bk.bucket_ms - (bk.bucket_ms % BAR_MS)

# ---- per-bar aggregates, for the trailing medians ----
bar = bk.groupby(["coin", "bar"], as_index=False).agg(
    ntl=("ntl", "sum"), n=("n", "sum"), ntl2=("ntl2", "sum"), big=("big_ntl", "sum"))
bar = bar.sort_values(["coin", "bar"])
bar["mean_sz"] = bar.ntl / bar.n.clip(lower=1)
g = bar.groupby("coin", sort=False)
bar["med_sz"] = g.mean_sz.transform(lambda s: s.shift(1).rolling(WIN, min_periods=24).median())
bar["med_n"] = g.n.transform(lambda s: s.shift(1).rolling(WIN, min_periods=24).median())
bidx = {(c, b): i for i, (c, b) in enumerate(zip(bar.coin.values, bar.bar.values))}
B = bar.reset_index(drop=True)

# ---- per-coin bucket arrays, for the trailing vpin windows ----
bk = bk.sort_values(["coin", "bucket_ms"])
per = {}
for c, gg in bk.groupby("coin", sort=False):
    per[c] = (gg.bucket_ms.values, gg.buy_ntl.values, gg.sell_ntl.values)


def flow(coin, end_ms, mins):
    """(vpin, ofi) over the `mins` ending at end_ms. Mirrors live_bot_ats.toxicity."""
    p = per.get(coin)
    if p is None:
        return (np.nan, np.nan)
    t, bu, se = p
    lo = np.searchsorted(t, end_ms - mins * 60000, "left")
    hi = np.searchsorted(t, end_ms, "left")
    if hi - lo < max(2, mins // 15):
        return (np.nan, np.nan)
    b, s = bu[lo:hi], se[lo:hi]
    den = float((b + s).sum())
    if den <= 0:
        return (np.nan, np.nan)
    return (float(np.abs(b - s).sum()) / den, float((b - s).sum()) / den)


rows = []
for r in ev.itertuples():
    end = r.bar_close_ms
    v30, _ = flow(r.sym, end, 30)
    v60, ofi = flow(r.sym, end, 60)
    i = bidx.get((r.sym, r.t))
    at = sc = bs = pr = np.nan
    if i is not None:
        b = B.iloc[i]
        if b.n > 0 and b.ntl > 0:
            mean_sz = b.ntl / b.n
            at = mean_sz / b.med_sz if b.med_sz and b.med_sz > 0 else np.nan
            sc = b.n * b.ntl2 / (b.ntl ** 2)
            bs = b.big / b.ntl
            pr = b.n / b.med_n if b.med_n and b.med_n > 0 else np.nan
    rows.append((v30, v60, np.nan if ofi != ofi else ofi * r.dirn, at, sc, bs, pr))
F = pd.DataFrame(rows, columns=["vpin30", "vpin60", "adverse_ofi",
                                "ats_tape", "size_conc", "big_share", "prints_ratio"])
ev = pd.concat([ev.reset_index(drop=True), F], axis=1)
FEATS = list(F.columns)
print("feature coverage: " + ", ".join(f"{f} {int(ev[f].notna().sum()):,}" for f in FEATS))

POPS = [("ALL SPIKES", ev), ("SIGNALLED (all gates)", ev[ev.signalled == 1])]


def st(v):
    v = np.asarray(v, float); v = v[~np.isnan(v)]
    n = len(v)
    return (v.mean(), v.mean() / (v.std(ddof=1) / math.sqrt(n)), n) if n > 1 else (np.nan,)*3


for pname, pop in POPS:
    print(f"\n{'='*78}\n### {pname} — n={len(pop):,}, "
          f"fade baseline {pop.fade_bps.mean():+.1f} bps\n{'='*78}")
    med_t = pop.t.median()
    for f in FEATS:
        s = pop[pop[f].notna()].copy()
        if len(s) < 200:
            print(f"\n--- {f}: only {len(s)} usable, skipping")
            continue
        s["q"] = pd.qcut(s[f], 5, labels=False, duplicates="drop")
        means = []
        print(f"\n--- {f}  (n={len(s):,}) ---")
        print(f"  {'quintile':>10} {'range':>18} {'n':>6} {'fade bps':>10} {'t':>6} "
              f"{'backstop':>9} {'blowup':>7}")
        for k, sub in s.groupby("q"):
            mu, t, n = st(sub.fade_bps)
            means.append(mu)
            print(f"  {int(k)+1:>10} {f'{sub[f].min():+.2f} to {sub[f].max():+.2f}':>18} "
                  f"{n:>6} {mu:>+10.1f} {t:>+6.1f} "
                  f"{100*(sub.why=='backstop').mean():>8.0f}% "
                  f"{100*(sub.fade_bps<-400).mean():>6.0f}%")
        ups = sum(1 for i in range(len(means)-1) if means[i+1] > means[i])
        gap = means[-1] - means[0]
        # both halves
        h1, h2 = s[s.t <= med_t], s[s.t > med_t]
        g1 = g2 = float("nan")
        for lab, sub in (("h1", h1), ("h2", h2)):
            if len(sub) > 100:
                k = len(sub) // 5
                ss = sub.sort_values(f)
                v = ss.fade_bps.values
                gg = v[4*k:].mean() - v[:k].mean()
                if lab == "h1":
                    g1 = gg
                else:
                    g2 = gg
        r = np.corrcoef(s[f], s.fade_bps)[0, 1]
        tr = r*math.sqrt((len(s)-2)/max(1e-12, 1-r*r))
        ok = (ups in (0, 4)) and (g1*g2 > 0) and abs(tr) >= 3
        print(f"  monotone {ups}/4 | Q5-Q1 {gap:+.1f} | halves {g1:+.0f} / {g2:+.0f} "
              f"| corr {r:+.3f} t={tr:+.2f}  -> {'PASS' if ok else 'fail'}")

ev.to_csv("tape_events_featured.csv", index=False)
print(f"\nwrote tape_events_featured.csv ({len(ev):,} rows)")
