#!/usr/bin/env python3
"""Does volume PERSISTING after the spike predict the rest of the trade? Causally.

volume_staircase.py found the sharpest gradient in this whole repo: bucketing fades by
average volume over the 8 bars AFTER entry gives +100.5, +71.3, +68.6, +18.1, -30.3 bps,
monotone, t=+9.3 in the top bucket. Volume that dies = exhaustion = the fade works.
Volume that persists = participation = the fade fails.

That number is not usable and must not be quoted. Two defects:

  LOOKAHEAD   it averages bars i+1..i+8 to explain a return that starts at bar i, so it
              knows the future of its own trade
  ENDOGENEITY volume and absolute price movement are the same event. A fade that is
              losing is, mechanically, a price that kept running, which prints volume.
              "High post-volume" is partly a restatement of "this trade lost", exactly
              the trap that made toxicity.py's label circular.

The fix for both is a clean split. Measure volume over the first PROBE bars only, then
measure the return from the end of the probe to the exit. The feature is then strictly in
the past of everything it predicts, and the decision it implies -- bail out, or flip -- is
one you could actually have taken at that moment.

  python3 analysis/volume_persist.py [probe_bars]
"""
import math, sys
import numpy as np
import pandas as pd

PROBE = int(sys.argv[1]) if len(sys.argv) > 1 else 2      # bars observed before deciding
WIN, BACKSTOP, MINBARS = 96, 32, 1500
VOL_MULT, RV_PCTILE, COST_BPS = 5.0, 0.60, 3.0

print(f"building events, probing {PROBE} bars ({PROBE*15}m) after entry ...")
df = pd.read_csv("hyperliquid_15m_allperps.csv").sort_values(
    ["symbol", "open_time_ms"]).reset_index(drop=True)
uni = pd.read_csv("perp_universe.csv").set_index("name")["day_notional_vol"]
q1 = uni.quantile(1/3)
fund = pd.read_csv("hyperliquid_funding.csv").sort_values(["symbol", "time_ms"])
fmap = {s: (g["time_ms"].values, g["funding_rate"].values)
        for s, g in fund.groupby("symbol", sort=False)}

rows = []
for sym, g in df.groupby("symbol", sort=False):
    if len(g) < MINBARS or uni.get(sym, 0) < q1:
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
        ats = vo/np.maximum(nt, 1)
        ats_r = ats/pd.Series(ats).shift(1).rolling(WIN).median().values
    brk = np.where(cl > ph, 1, np.where(cl < pl, -1, 0))
    ft, fr = fmap.get(sym, (None, None))
    if ft is None:
        continue
    for i in np.nonzero((vr >= VOL_MULT) & (brk != 0) & ~np.isnan(rv))[0]:
        if i + BACKSTOP + 2 >= len(cl) or i < 8:
            continue
        j = np.searchsorted(ft, tm[i], side="right") - 1
        if j < 0 or (1 if fr[j] > 0 else (-1 if fr[j] < 0 else 0)) != brk[i]:
            continue
        b = int(brk[i]); d = -b; entry = cl[i]

        # ---- FEATURE: volume over the probe window only, vs the signal bar ----
        probe_v = float(np.mean(vo[i+1:i+1+PROBE]))/max(vo[i], 1e-9)
        # price travelled during the probe, so we can control for it
        probe_r = d*(cl[i+PROBE]-entry)/entry*1e4

        # ---- full fade, as the bot trades it today ----
        full = None
        for k in range(1, BACKSTOP+1):
            c = cl[i+k]
            if (d < 0 and c < ph[i]) or (d > 0 and c > pl[i]):
                full = d*(c-entry)/entry; break
        why = "reclaim" if full is not None else "backstop"
        if full is None:
            full = d*(cl[i+BACKSTOP]-entry)/entry
        full = full*1e4 - COST_BPS

        # ---- REMAINING fade return, from the end of the probe onward ----
        base = cl[i+PROBE]
        rest = None
        for k in range(PROBE+1, BACKSTOP+1):
            c = cl[i+k]
            if (d < 0 and c < ph[i]) or (d > 0 and c > pl[i]):
                rest = d*(c-base)/base; break
        if rest is None:
            rest = d*(cl[i+BACKSTOP]-base)/base
        rest = rest*1e4
        # ---- and the same window traded the OTHER way (momentum) ----
        flip = {h: b*(cl[i+PROBE+h]-base)/base*1e4 - COST_BPS
                for h in (2, 4, 8, 16) if i+PROBE+h < len(cl)}
        rows.append(dict(t=tm[i], sym=sym, rv=rv[i], ats_r=ats_r[i], why=why,
                         pv=probe_v, pr=probe_r, full=full, rest=rest,
                         **{f"m{h}": v for h, v in flip.items()}))

ev = pd.DataFrame(rows).sort_values("t").reset_index(drop=True)
ev = ev[ev["rv"] >= ev["rv"].quantile(RV_PCTILE)].reset_index(drop=True)
ev["mo"] = pd.to_datetime(ev["t"], unit="ms").dt.strftime("%Y-%m")
n = len(ev)
print(f"events {n:,}   full-trade baseline {ev.full.mean():+.1f} bps   "
      f"post-probe remainder {ev.rest.mean():+.1f} bps\n")


def st(v):
    v = np.asarray(v, float); v = v[~np.isnan(v)]
    k = len(v)
    return (v.mean(), v.mean()/(v.std(ddof=1)/math.sqrt(k)), k) if k > 1 else (np.nan,)*3


ev["q"] = pd.qcut(ev.pv, 5, labels=False, duplicates="drop")
print(f"=== volume in the first {PROBE} bars, vs what happens AFTERWARDS ===")
print("  (feature strictly precedes the return -- no lookahead)")
print(f"  {'quintile':>10} {'vol vs spike':>13} {'n':>6} {'probe ret':>10} "
      f"{'REST of fade':>13} {'t':>6} {'backstop':>9} {'blowup':>8}")
for k, s in ev.groupby("q"):
    m, t, kk = st(s.rest)
    print(f"  {int(k)+1:>10} {s.pv.mean():>13.2f} {kk:>6} {s.pr.mean():>+10.1f} "
          f"{m:>+13.1f} {t:>+6.1f} {100*(s.why=='backstop').mean():>8.0f}% "
          f"{100*(s.full < -400).mean():>7.0f}%")

print("\n=== controlling for endogeneity: same split, but WITHIN probe-return buckets ===")
print("  if volume only proxies 'the trade is already losing', the gradient vanishes here")
ev["pq"] = pd.qcut(ev.pr, 3, labels=["probe losing", "probe flat", "probe winning"],
                   duplicates="drop")
print(f"  {'probe outcome':>16} {'low-vol rest':>14} {'high-vol rest':>15} {'gap':>9} {'n':>12}")
for pq, s in ev.groupby("pq", observed=True):
    loq, hiq = s[s.q <= 1], s[s.q >= 3]
    if len(loq) < 20 or len(hiq) < 20:
        continue
    a, b = loq.rest.mean(), hiq.rest.mean()
    print(f"  {str(pq):>16} {a:>+14.1f} {b:>+15.1f} {a-b:>+9.1f} "
          f"{f'{len(loq)}/{len(hiq)}':>12}")

print(f"\n=== the tradeable rule: bail out after {PROBE} bars if volume has not died ===")
print(f"  {'rule':>34} {'n exited':>9} {'total bps':>11} {'vs hold':>10} {'blowups':>9}")
base = ev.full.sum()
print(f"  {'hold everything (today)':>34} {0:>9} {base:>+11,.0f} {0:>+10,.0f} "
      f"{int((ev.full<-400).sum()):>9}")
for thr in (0.5, 0.7, 0.9, 1.2):
    cut = ev.pv > thr
    # exited trades realise only the probe return, less an extra taker exit
    tot = ev.loc[~cut, "full"].sum() + (ev.loc[cut, "pr"] - COST_BPS).sum()
    blow = int((ev.loc[~cut, "full"] < -400).sum() + (ev.loc[cut, "pr"] < -400).sum())
    print(f"  {f'exit if probe vol > {thr:.1f}x spike':>34} {int(cut.sum()):>9} "
          f"{tot:>+11,.0f} {tot-base:>+10,.0f} {blow:>9}")

print(f"\n=== FLIP instead: go WITH the move when volume persists ===")
print(f"  {'subset':>28} {'n':>6} " + " ".join(f"{'+'+str(h)+'b':>11}" for h in (2, 4, 8, 16)))
for lab, s in (("all", ev), ("probe vol > 0.9x", ev[ev.pv > 0.9]),
               ("probe vol > 0.9x, ats<2", ev[(ev.pv > 0.9) & (ev.ats_r < 2)]),
               ("probe vol > 1.2x", ev[ev.pv > 1.2])):
    if len(s) < 30:
        continue
    cells = []
    for h in (2, 4, 8, 16):
        m, t, k = st(s[f"m{h}"])
        cells.append(f"{m:>+6.0f}/{t:>+.1f}")
    print(f"  {lab:>28} {len(s):>6} " + " ".join(f"{c:>11}" for c in cells))

print("\n=== CONCENTRATION by month, top-vs-bottom volume quintile gap ===")
for mo, s in ev.groupby("mo"):
    loq, hiq = s[s.q <= 1], s[s.q >= 3]
    if len(loq) < 20 or len(hiq) < 20:
        continue
    print(f"  {mo}  n={len(s):>4}  low-vol {loq.rest.mean():>+8.1f}  "
          f"high-vol {hiq.rest.mean():>+8.1f}  gap {loq.rest.mean()-hiq.rest.mean():>+8.1f}")
print("\n  A gradient present in every month is a property of the strategy.")
print("  One that lives in a single month is the pattern that has failed here five times.")
