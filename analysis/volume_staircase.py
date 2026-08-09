#!/usr/bin/env python3
"""Escalating volume into a breakout: does the "staircase" predict continuation?

SAGA, the -$10.09 liquidation, did not look like the spikes this strategy is built to
fade. A fade wants ONE isolated print into a thin book that then exhausts. SAGA ramped:
volume climbed across consecutive 15m bars while price climbed with it (126k -> 642k ->
6,535k notional into the signal bar), and after the entry it re-accelerated rather than
decaying, ending with a bar as large as the original spike seven hours later.

That is the visual shape of participation building, not of a single dislocation. It is
also consistent with what ats_tail.py found: low avg-trade-size spikes -- many small
trades rather than one whale -- are where the blowups live.

So test it properly. Two questions, and they are different:

  1. FILTER  does escalation identify breakouts that should NOT be faded?
  2. FOLLOW  is there a positive edge in trading WITH an escalating breakout?

A filter only has to spot bad fades. Following requires a real edge in the other
direction and is a much stronger claim, so it gets the stricter reading.

Features, all computable at signal time from closed bars only:
  run_len   consecutive prior bars with volume increasing
  esc       vol[i] / vol[i-1], the final step up
  ramp      vol[i] / max(vol[i-4..i-1]), is this bar the biggest of the local run
  aligned   consecutive bars where volume rose AND price moved the breakout way
  pre_move  fraction of the 24h range already travelled during the run-up

  python3 analysis/volume_staircase.py [15m|1h]
"""
import math, sys
import numpy as np
import pandas as pd

IV = sys.argv[1] if len(sys.argv) > 1 else "15m"
CFG = {"15m": ("hyperliquid_15m_allperps.csv", 96, 32, 1500),
       "1h": ("hyperliquid_1h_history.csv", 24, 8, 400)}
CANDLES, WIN, BACKSTOP, MINBARS = CFG[IV]
VOL_MULT, RV_PCTILE, COST_BPS = 5.0, 0.60, 3.0

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
    nt = g["num_trades"].values.astype(float); tm = g["open_time_ms"].values
    med = pd.Series(vo).shift(1).rolling(WIN).median().values
    ph = pd.Series(hi).shift(1).rolling(WIN).max().values
    pl = pd.Series(lo).shift(1).rolling(WIN).min().values
    lr = np.full(len(cl), np.nan); lr[1:] = np.log(cl[1:]/cl[:-1])
    rv = pd.Series(lr).rolling(WIN).std().values
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = vo/med
        ats = (vo/np.maximum(nt, 1))
        ats_r = ats/pd.Series(ats).shift(1).rolling(WIN).median().values
    brk = np.where(cl > ph, 1, np.where(cl < pl, -1, 0))
    ft, fr = fmap.get(sym, (None, None))
    if ft is None:
        continue
    for i in np.nonzero((vr >= VOL_MULT) & (brk != 0) & ~np.isnan(rv))[0]:
        if i + BACKSTOP >= len(cl) or i < 8:
            continue
        j = np.searchsorted(ft, tm[i], side="right") - 1
        if j < 0 or (1 if fr[j] > 0 else (-1 if fr[j] < 0 else 0)) != brk[i]:
            continue
        b = int(brk[i])
        # --- the staircase, from closed bars only ---
        run = 0
        for k in range(1, 7):
            if vo[i-k+1] > vo[i-k]:
                run += 1
            else:
                break
        aligned = 0
        for k in range(1, 7):
            up = vo[i-k+1] > vo[i-k]
            dirn = (cl[i-k+1] - cl[i-k]) * b > 0
            if up and dirn:
                aligned += 1
            else:
                break
        esc = vo[i]/max(vo[i-1], 1e-9)
        ramp = vo[i]/max(np.max(vo[i-4:i]), 1e-9)
        rng = max(ph[i] - pl[i], 1e-12)
        pre = (cl[i-1] - cl[i-1-max(run, 1)]) * b / rng

        # --- outcome of the FADE (what the bot does today) ---
        d = -b; entry = cl[i]; ret = None
        for k in range(1, BACKSTOP+1):
            c = cl[i+k]
            if (d < 0 and c < ph[i]) or (d > 0 and c > pl[i]):
                ret = d*(c-entry)/entry; break
        why = "reclaim" if ret is not None else "backstop"
        if ret is None:
            ret = d*(cl[i+BACKSTOP]-entry)/entry
        # --- outcome of FOLLOWING, at several horizons ---
        follow = {h: b*(cl[i+h]-entry)/entry*1e4 - COST_BPS
                  for h in (2, 4, 8, 16, 32) if i+h < len(cl)}
        # --- volume AFTER entry: did it decay (exhaustion) or re-accelerate? ---
        post = vo[i+1:i+9]
        post_ratio = float(np.mean(post))/max(vo[i], 1e-9) if len(post) else np.nan
        rows.append(dict(t=tm[i], sym=sym, rv=rv[i], b=b, run=run, aligned=aligned,
                         esc=esc, ramp=ramp, pre=pre, ats_r=ats_r[i], vr=vr[i],
                         y=ret*1e4 - COST_BPS, why=why, post=post_ratio, **{f"f{h}": v for h, v in follow.items()}))

ev = pd.DataFrame(rows).sort_values("t").reset_index(drop=True)
ev = ev[ev["rv"] >= ev["rv"].quantile(RV_PCTILE)].reset_index(drop=True)
ev["mo"] = pd.to_datetime(ev["t"], unit="ms").dt.strftime("%Y-%m")
n = len(ev)
print(f"events: {n:,}  coins: {ev.sym.nunique()}  baseline fade {ev.y.mean():+.1f} bps, "
      f"backstop {100*(ev.why=='backstop').mean():.0f}%\n")


def st(v):
    v = np.asarray(v, dtype=float); v = v[~np.isnan(v)]
    k = len(v)
    if k < 2:
        return (float("nan"), float("nan"), k)
    return (v.mean(), v.mean()/(v.std(ddof=1)/math.sqrt(k)), k)


def buckets(col, label, edges=None):
    print(f"--- {label} ---")
    print(f"  {'bucket':>16} {'n':>6} {'fade bps':>10} {'t':>6} {'backstop':>9} "
          f"{'blowup':>7} {'post-vol':>9}")
    if edges is None:
        qs_ = ev[col].quantile([.2, .4, .6, .8]).values
        edges = list(dict.fromkeys(np.round(qs_, 3)))
    prev = -1e18
    for e in list(edges) + [1e18]:
        seg = ev[(ev[col] > prev) & (ev[col] <= e)]
        if len(seg) < 30:
            prev = e; continue
        m, t, k = st(seg.y)
        print(f"  {f'{prev if prev>-1e17 else 0:g}-{e if e<1e17 else 999:g}':>16} {k:>6} "
              f"{m:>+10.1f} {t:>+6.1f} {100*(seg.why=='backstop').mean():>8.0f}% "
              f"{100*(seg.y < -400).mean():>6.0f}% {seg.post.mean():>9.2f}")
        prev = e
    print()


print("=== 1. FILTER: does the staircase mark fades that fail? ===")
buckets("aligned", "consecutive bars of RISING VOLUME + price moving the breakout way",
        edges=[0, 1, 2, 3])
buckets("run", "consecutive bars of rising volume (ignoring price)", edges=[0, 1, 2, 3])
buckets("ramp", "signal bar vs the biggest of the previous 4")
buckets("post", "volume AFTER entry, as a fraction of the signal bar (in-trade, not a filter)")

print("=== 2. the obvious filter, priced ===")
base_tot = ev.y.sum()
print(f"  {'rule':>34} {'kept':>7} {'bps':>9} {'t':>6} {'total':>10} {'vs base':>9} {'blowups':>8}")
print(f"  {'no filter':>34} {n:>7} {ev.y.mean():>+9.1f} {st(ev.y)[1]:>+6.1f} "
      f"{base_tot:>+10,.0f} {0:>+9,.0f} {int((ev.y<-400).sum()):>8}")
for lab, mask in (("skip aligned>=2", ev.aligned < 2), ("skip aligned>=3", ev.aligned < 3),
                  ("skip run>=3", ev.run < 3),
                  ("skip aligned>=2 AND low ats", ~((ev.aligned >= 2) & (ev.ats_r < 2))),
                  ("skip low ats only (<2)", ev.ats_r >= 2)):
    k = ev[mask]
    m, t, kk = st(k.y)
    print(f"  {lab:>34} {kk:>7} {m:>+9.1f} {t:>+6.1f} {k.y.sum():>+10,.0f} "
          f"{k.y.sum()-base_tot:>+9,.0f} {int((k.y<-400).sum()):>8}")

print("\n=== 3. FOLLOW: is there an edge trading WITH an escalating breakout? ===")
print("  a filter only needs to spot bad fades; this needs a real edge the other way")
print(f"  {'subset':>24} {'n':>6} " + " ".join(f"{'+'+str(h)+'b':>9}" for h in (2, 4, 8, 16, 32)))
for lab, seg in (("all breakouts", ev), ("aligned>=2", ev[ev.aligned >= 2]),
                 ("aligned>=3", ev[ev.aligned >= 3]),
                 ("aligned>=2 & ats<2", ev[(ev.aligned >= 2) & (ev.ats_r < 2)]),
                 ("aligned>=2 & post>0.5", ev[(ev.aligned >= 2) & (ev.post > 0.5)])):
    if len(seg) < 30:
        continue
    cells = []
    for h in (2, 4, 8, 16, 32):
        m, t, k = st(seg[f"f{h}"])
        cells.append(f"{m:>+6.0f}/{t:>+.1f}")
    print(f"  {lab:>24} {len(seg):>6} " + " ".join(f"{c:>9}" for c in cells))
print("  cells are mean bps / t-stat. Costs already netted at 3bps round trip.")

print("\n=== 4. CONCENTRATION: the test that has killed every other filter here ===")
best = ev[ev.aligned >= 2]
print(f"  'skip aligned>=2' saves {base_tot - ev[ev.aligned < 2].y.sum():+,.0f} bps by "
      f"dropping {len(best)} events. Where does that come from?")
g = best.groupby("mo").y.agg(["sum", "size"])
tot = g["sum"].sum()
for mo, r in g.iterrows():
    print(f"    {mo}  n={int(r['size']):>4}  {r['sum']:>+9,.0f} bps  "
          f"({100*r['sum']/tot if tot else float('nan'):>+5.0f}% of it)")
print("\n  A filter whose value is one month is a coincidence with a story attached.")
