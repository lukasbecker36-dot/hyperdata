#!/usr/bin/env python3
"""Market-regime filter: does the fade work better when aligned with the trend?

CONCLUSION (see commit history): NO USABLE FILTER HERE. Do not deploy any variant of this.
The full-sample numbers look attractive and the aggregate survives a time split, but a
per-month breakdown of the long-side filter shows the entire effect is ONE MONTH:
  skip LONG when coin above its own 20dMA : total +4,435 bps, of which January is +3,683
                                            (83%); ex-January +752 = +1.7% of baseline
  skip LONG when BTC above its 20dMA      : total +3,858, January +3,734 (97%);
                                            ex-January +124 = +0.3%, i.e. nothing
The excluded group is only 44-78 trades over 186 days -- about 6-11 per month, with monthly
n as low as 1 -- so monthly means are unestimable against 300+ bps of noise. The sign also
flips in April and May. Aggregate-level reporting hid all of this; check concentration
BEFORE recommending anything.

Hypothesis to test: a DIP in an uptrend is more likely to bounce (so fade it long), and a
SPIKE in a downtrend is more likely to fail (so fade it short). The opposite is equally
plausible -- a spike in an uptrend could be a blow-off top -- so both directions are
measured rather than assumed.

    trade direction:  fading a DOWN-break = LONG,  fading an UP-break = SHORT
    trend-ALIGNED  :  LONG while above the MA,  SHORT while below it
    trend-AGAINST  :  the reverse

Two flavours of regime, as asked:
    BTC   one market-wide switch -- BTC vs its own MA, applied to every coin
    OWN   each coin vs its own MA

Both at 20/50/100-day lookbacks, and both as a binary side AND as distance from the MA,
since a coin 1% above its average is not in the same state as one 40% above.

Run on the 1h/211-day history: a 50-day MA needs 1,200 hourly bars of warm-up, which would
consume almost the entire 52-day 15m sample. Gates and exits replicate the live arm (5x
spike, 24h breakout, rv above the 60th pct, funding sign aligned, HIGH+MID; exit on reclaim
or the 8h backstop).

  python3 analysis/regime_filter.py
"""
import numpy as np
import pandas as pd

CANDLES, WIN, BACKSTOP, MINBARS = "hyperliquid_1h_history.csv", 24, 8, 400
VOL_MULT, RV_PCTILE, COST_BPS = 5.0, 0.60, 3.0
import sys
# Requiring several lookbacks at once forces every event to wait for the LONGEST warm-up
# (a 100-day MA needs 2,400 hourly bars), which silently truncated the first run to 107
# days and 4 usable months. Pass the lookbacks you want so the sample is not throttled by
# one you are only curious about.
MA_DAYS = ([int(x) for x in sys.argv[1].split(",")] if len(sys.argv) > 1 else [20, 50, 100])
OOS_CUT = pd.Timestamp("2026-05-26", tz="UTC").value // 10**6

print("loading ...")
df = pd.read_csv(CANDLES).sort_values(["symbol", "open_time_ms"]).reset_index(drop=True)
uni = pd.read_csv("perp_universe.csv").set_index("name")["day_notional_vol"]
qs = uni.quantile([1/3, 2/3]).values
fund = pd.read_csv("hyperliquid_funding.csv").sort_values(["symbol", "time_ms"])
fmap = {s: (g["time_ms"].values, g["funding_rate"].values)
        for s, g in fund.groupby("symbol", sort=False)}

# ---- BTC reference trend ----
btc = df[df.symbol == "BTC"].reset_index(drop=True)
bt, bc = btc["open_time_ms"].values, btc["close"].values.astype(float)
bma = {d: pd.Series(bc).rolling(d*24).mean().values for d in MA_DAYS}
print(f"BTC reference: {len(bc)} hourly bars")

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
    oma = {d: pd.Series(cl).rolling(d*24).mean().values for d in MA_DAYS}
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
        k = np.searchsorted(bt, tm[i], side="right") - 1
        if k < 0:
            continue
        d = -int(brk[i]); entry = cl[i]; ret = None
        for s in range(1, BACKSTOP+1):
            c = cl[i+s]
            if (d < 0 and c < ph[i]) or (d > 0 and c > pl[i]):
                ret = d*(c-entry)/entry; break
        if ret is None:
            ret = d*(cl[i+BACKSTOP]-entry)/entry
        r = dict(t=tm[i], sym=sym, dirn=d, rv=rv[i], y=ret*1e4 - COST_BPS)
        ok = True
        for dd in MA_DAYS:
            bm, om = bma[dd][k], oma[dd][i]
            if np.isnan(bm) or np.isnan(om) or bm <= 0 or om <= 0:
                ok = False; break
            r[f"btc{dd}"] = (bc[k]/bm - 1.0)*100      # % above/below BTC's MA
            r[f"own{dd}"] = (entry/om - 1.0)*100      # % above/below its own MA
        if ok:
            rows.append(r)
ev = pd.DataFrame(rows).sort_values("t").reset_index(drop=True)
ev = ev[ev["rv"] >= ev["rv"].quantile(RV_PCTILE)].reset_index(drop=True)
days = (ev["t"].max()-ev["t"].min())/86400000
print(f"events: {len(ev):,} over {days:.0f} days "
      f"({(ev.dirn > 0).sum()} LONG / {(ev.dirn < 0).sum()} SHORT)")
base = ev["y"].mean()
print(f"baseline: {base:+.1f} bps/trade, total {ev['y'].sum():+,.0f}\n")


def st(x):
    n = len(x)
    if n < 2: return (float("nan"), float("nan"), n)
    m = x.mean(); sd = x.std()
    return (m, m/(sd/np.sqrt(n)) if sd > 0 else float("nan"), n)


print("=== base rates: how much of the sample is each regime? ===")
for dd in MA_DAYS:
    for src in ("btc", "own"):
        up = (ev[f"{src}{dd}"] > 0).mean()*100
        print(f"  {src}{dd}: above MA {up:>3.0f}% of events")
print()

for dd in MA_DAYS:
    for src, lab in (("btc", "BTC market-wide"), ("own", "coin's own")):
        col = f"{src}{dd}"
        print(f"=== {lab} trend, {dd}-day MA ===")
        print(f"  {'':>16} {'n':>5} {'bps/trade':>10} {'t':>6}")
        for dname, dsel in (("LONG  (fade dip)", ev.dirn > 0), ("SHORT (fade spike)", ev.dirn < 0)):
            for rname, rsel in ((f"above MA", ev[col] > 0), (f"below MA", ev[col] <= 0)):
                s = ev[dsel & rsel]["y"]
                if len(s) < 20:
                    print(f"  {dname[:6]:>6} {rname:>9} {len(s):>5}  too few"); continue
                m, t, n = st(s)
                print(f"  {dname[:6]:>6} {rname:>9} {n:>5} {m:>+10.1f} {t:>+6.1f}")
        al = ev[((ev.dirn > 0) & (ev[col] > 0)) | ((ev.dirn < 0) & (ev[col] <= 0))]["y"]
        ag = ev[((ev.dirn > 0) & (ev[col] <= 0)) | ((ev.dirn < 0) & (ev[col] > 0))]["y"]
        ma_, ta, na = st(al); mg, tg, ng = st(ag)
        print(f"    trend-ALIGNED {na:>5} {ma_:>+10.1f} {ta:>+6.1f}   "
              f"(total {al.sum():>+8,.0f} = {al.sum()/ev['y'].sum()*100:>3.0f}% of base)")
        print(f"    trend-AGAINST {ng:>5} {mg:>+10.1f} {tg:>+6.1f}   "
              f"(total {ag.sum():>+8,.0f} = {ag.sum()/ev['y'].sum()*100:>3.0f}% of base)")
        print(f"    aligned minus against: {ma_-mg:+.1f} bps/trade\n")

print("=== best-looking variant, checked out of sample ===")
scores = {}
for dd in MA_DAYS:
    for src in ("btc", "own"):
        col = f"{src}{dd}"
        al = ev[((ev.dirn > 0) & (ev[col] > 0)) | ((ev.dirn < 0) & (ev[col] <= 0))]["y"]
        ag = ev[((ev.dirn > 0) & (ev[col] <= 0)) | ((ev.dirn < 0) & (ev[col] > 0))]["y"]
        scores[col] = st(al)[0] - st(ag)[0]
bestcol = max(scores, key=lambda c: abs(scores[c]))
print(f"  largest aligned-minus-against spread: {bestcol} ({scores[bestcol]:+.1f} bps)")
for lbl, sub in (("full sample", ev), ("pre 2026-05-26 (OOS)", ev[ev.t < OOS_CUT]),
                 ("post 2026-05-26", ev[ev.t >= OOS_CUT])):
    c = bestcol
    al = sub[((sub.dirn > 0) & (sub[c] > 0)) | ((sub.dirn < 0) & (sub[c] <= 0))]["y"]
    ag = sub[((sub.dirn > 0) & (sub[c] <= 0)) | ((sub.dirn < 0) & (sub[c] > 0))]["y"]
    if len(al) < 20 or len(ag) < 20:
        print(f"  {lbl:>22}: too few"); continue
    print(f"  {lbl:>22}: aligned {st(al)[0]:+7.1f} (n={len(al):>4})  "
          f"against {st(ag)[0]:+7.1f} (n={len(ag):>4})  spread {st(al)[0]-st(ag)[0]:+7.1f}")

print("\n=== monthly persistence of that variant ===")
ev["mo"] = pd.to_datetime(ev["t"], unit="ms", utc=True).dt.strftime("%Y-%m")
c = bestcol
print(f"  {'month':>9} {'n_al':>5} {'aligned':>9} {'n_ag':>5} {'against':>9} {'spread':>8}")
pos = 0; tot = 0
for mo, g in ev.groupby("mo"):
    al = g[((g.dirn > 0) & (g[c] > 0)) | ((g.dirn < 0) & (g[c] <= 0))]["y"]
    ag = g[((g.dirn > 0) & (g[c] <= 0)) | ((g.dirn < 0) & (g[c] > 0))]["y"]
    if len(al) < 8 or len(ag) < 8:
        continue
    sp = al.mean()-ag.mean(); tot += 1; pos += 1 if sp > 0 else 0
    print(f"  {mo:>9} {len(al):>5} {al.mean():>+9.1f} {len(ag):>5} {ag.mean():>+9.1f} {sp:>+8.1f}")
print(f"  spread positive in {pos}/{tot} months")
print("\nA regime filter is only real if the aligned-minus-against spread is large, holds")
print("out of sample, and is positive in most months -- not just on the full-sample average.")
