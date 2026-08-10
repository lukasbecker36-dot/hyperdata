#!/usr/bin/env python3
"""Should the rv gate move? Priced on the marginal band, not the whole threshold.

tape_features.py found rv is the strongest predictor in the whole feature set and that
rv >= 40th pct kept 2,853 events for +47,762 bps against 1,902 for +41,597 at the 60th.
That is not enough to act on, for three reasons this script fixes:

  MARGINAL BAND   "gate at 40 vs 60" bundles trades already taken with new ones, so the
                  comparison is dominated by trades the change does not affect. The real
                  question is only what the 40-60 band earns. Everything else is noise
                  imported from both sides.
  COSTS           the tape run charged a flat 3bps. Live fees are 5.76bps round trip when
                  crossing and 2.83 when resting, and low-rv coins have TIGHTER spreads,
                  so the bot crosses them and pays the higher rate. The thinner edge gets
                  the worse cost, which is exactly the interaction that decides this.
  ROLLING RANK    ranking rv against the whole sample is lookahead. The bot recalibrates
                  from the last 15 days of signals (`calibrated rv threshold = 0.003577
                  from 2509 signals`), so the backtest must rank the same way.

Also applies the full gate set -- funding alignment and HIGH/MID tier -- because the
population the change would actually add to is the gated one, not all spikes.

  python3 analysis/rv_gate.py [cost_bps]
"""
import math, sys
import numpy as np
import pandas as pd

COST = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
WIN, BACKSTOP, MINBARS = 96, 32, 1500
VOL_MULT = 5.0
CALIB_DAYS = 15

print(f"building events, charging {COST:.1f}bps round trip ...")
df = pd.read_csv("hyperliquid_15m_allperps.csv").sort_values(
    ["symbol", "open_time_ms"]).reset_index(drop=True)
uni = pd.read_csv("perp_universe.csv").set_index("name")["day_notional_vol"]
qs = uni.quantile([1/3, 2/3]).values
tier = {s: ("LOW" if v < qs[0] else ("MID" if v < qs[1] else "HIGH"))
        for s, v in uni.items()}
fund = pd.read_csv("hyperliquid_funding.csv").sort_values(["symbol", "time_ms"])
fmap = {s: (g["time_ms"].values, g["funding_rate"].values)
        for s, g in fund.groupby("symbol", sort=False)}

rows = []
for sym, g in df.groupby("symbol", sort=False):
    if len(g) < MINBARS or tier.get(sym, "LOW") == "LOW":
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
        b = int(brk[i]); d = -b; entry = cl[i]
        ret = None
        for k in range(1, BACKSTOP+1):
            c = cl[i+k]
            if (d < 0 and c < ph[i]) or (d > 0 and c > pl[i]):
                ret = d*(c-entry)/entry; break
        why = "reclaim" if ret is not None else "backstop"
        if ret is None:
            ret = d*(cl[i+BACKSTOP]-entry)/entry
        rows.append(dict(t=tm[i], sym=sym, rv=float(rv[i]), why=why,
                         gross=float(ret*1e4)))

ev = pd.DataFrame(rows).sort_values("t").reset_index(drop=True)
ev["net"] = ev.gross - COST
ev["mo"] = pd.to_datetime(ev.t, unit="ms").dt.strftime("%Y-%m")

# ---- ROLLING rv percentile: rank each event against the prior CALIB_DAYS of signals ----
win_ms = CALIB_DAYS * 86400000
t = ev.t.values; r = ev.rv.values
pct = np.full(len(ev), np.nan)
for i in range(len(ev)):
    lo_i = np.searchsorted(t, t[i] - win_ms, "left")
    if i - lo_i < 100:
        continue
    prior = r[lo_i:i]
    pct[i] = (prior < r[i]).mean()
ev["pct"] = pct
ok = ev[ev.pct.notna()].copy()
print(f"{len(ev):,} gated events, {len(ok):,} with a rolling rv rank "
      f"({ok.mo.min()} to {ok.mo.max()})\n")


def st(v):
    v = np.asarray(v, float); n = len(v)
    return (v.mean(), v.mean()/(v.std(ddof=1)/math.sqrt(n)), n) if n > 1 else (np.nan,)*3


print("=== 1. THE MARGINAL BANDS: what does each 10-percentile slice of rv earn? ===")
print(f"  {'band':>12} {'n':>6} {'gross':>9} {'net':>9} {'t':>6} {'backstop':>9} "
      f"{'blowup':>7} {'net total':>11}")
for a in range(0, 100, 10):
    s = ok[(ok.pct >= a/100) & (ok.pct < (a+10)/100)]
    if len(s) < 30:
        continue
    m, tt, n = st(s.net.values)
    print(f"  {f'{a}-{a+10}th':>12} {n:>6} {s.gross.mean():>+9.1f} {m:>+9.1f} {tt:>+6.1f} "
          f"{100*(s.why=='backstop').mean():>8.0f}% {100*(s.net<-400).mean():>6.0f}% "
          f"{s.net.sum():>+11,.0f}")

print(f"\n=== 2. THE ACTUAL DECISION: is the 40-60 band worth adding? ===")
band = ok[(ok.pct >= 0.40) & (ok.pct < 0.60)]
cur = ok[ok.pct >= 0.60]
m, tt, n = st(band.net.values)
mc, tc, nc = st(cur.net.values)
print(f"  currently traded (>=60th) n={nc:<5} {mc:>+7.1f} bps net  t={tc:>+5.1f}  "
      f"total {cur.net.sum():>+9,.0f}")
print(f"  the 40-60 band          n={n:<5} {m:>+7.1f} bps net  t={tt:>+5.1f}  "
      f"total {band.net.sum():>+9,.0f}")
print(f"  -> adding it changes total by {band.net.sum():+,.0f} bps "
      f"({100*band.net.sum()/cur.net.sum():+.0f}%) and trade count by "
      f"{100*n/nc:+.0f}%")

print(f"\n=== 3. COST SENSITIVITY: the band's edge is thin, so what kills it? ===")
print(f"  {'cost bps':>10} {'40-60 band net':>16} {'t':>7} {'>=60th net':>12}")
for c in (3.0, 5.0, 5.8, 7.0, 9.0):
    bn = band.gross.mean() - c
    cn = cur.gross.mean() - c
    sd = band.gross.std(ddof=1)
    print(f"  {c:>10.1f} {bn:>+16.1f} {bn/(sd/math.sqrt(len(band))):>+7.1f} {cn:>+12.1f}")
print("  live fees are 5.76bps crossing / 2.83 resting. Low-rv coins have tighter")
print("  spreads, so the added trades skew toward the crossing rate.")

print(f"\n=== 4. STABILITY: does the band pay in every month, or one? ===")
g = band.groupby("mo").net.agg(["mean", "sum", "size"])
tot = g["sum"].sum()
for mo, r_ in g.iterrows():
    if r_["size"] < 20:
        continue
    print(f"  {mo}  n={int(r_['size']):>4}  {r_['mean']:>+8.1f} bps  "
          f"{r_['sum']:>+9,.0f} total  ({100*r_['sum']/tot if tot else float('nan'):>+5.0f}% of it)")

print(f"\n=== 5. HOLDOUT: split the history in half by time ===")
med_t = ok.t.median()
for lab, s in (("first half", band[band.t <= med_t]), ("second half", band[band.t > med_t])):
    if len(s) < 30:
        continue
    m, tt, n = st(s.net.values)
    print(f"  {lab:>12} n={n:<5} {m:>+7.1f} bps net  t={tt:>+5.1f}")
print("\n  Decision rule: add the band only if it is positive after 5.8bps, positive in")
print("  BOTH halves, and not concentrated in one month. Otherwise it is a coin flip")
print("  that costs real fees on every extra trade.")
